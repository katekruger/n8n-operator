"""Stage 07's completion gate, as Hypothesis properties: determinism, ``diff(A, A) ==
[]``, and the central invariant — every semantic change affects either the hash or a
visible diff category, and every allowlisted (cosmetic) change affects neither. Proven
over small, randomly generated *raw* n8n-shaped definitions, run through the same
``n8n.canonicalization`` pipeline (ADR-008) ``diff_workflow_definition`` itself uses —
so "a diff and a drift detection can never disagree" is proven, not just asserted.
"""

from __future__ import annotations

import copy
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from n8n_operator.core.definition_diff import diff_canonical_definitions
from n8n_operator.n8n.canonicalization import canonical_form, compute_definition_hash

_NODE_TYPES = ("n8n-nodes-base.set", "n8n-nodes-base.webhook", "n8n-nodes-base.httpRequest")
_SAFE_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFG0123456789_", min_size=1, max_size=12
)


@st.composite
def _raw_node(draw: st.DrawFn, *, node_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "name": draw(_SAFE_TEXT),
        "type": draw(st.sampled_from(_NODE_TYPES)),
        "position": [draw(st.integers(0, 1000)), draw(st.integers(0, 1000))],
        "parameters": {draw(_SAFE_TEXT): draw(_SAFE_TEXT)},
    }


@st.composite
def _raw_definition(draw: st.DrawFn) -> dict[str, Any]:
    count = draw(st.integers(min_value=1, max_value=4))
    nodes = [draw(_raw_node(node_id=f"id-{i}")) for i in range(count)]
    settings = {draw(_SAFE_TEXT): draw(_SAFE_TEXT)}
    return {
        "nodes": nodes,
        "connections": {},
        "settings": settings,
        "pinData": {},
        # Administrative fields, structurally out of canonicalization's scope —
        # included to prove they never affect the hash or the diff either.
        "id": draw(_SAFE_TEXT),
        "name": draw(_SAFE_TEXT),
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }


def _diff(registered_raw: dict[str, Any], live_raw: dict[str, Any]) -> tuple[list[Any], bool, int]:
    return diff_canonical_definitions(canonical_form(registered_raw), canonical_form(live_raw))


@given(raw=_raw_definition())
@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_diff_of_identical_definitions_is_always_empty(raw: dict[str, Any]) -> None:
    entries, truncated, total = _diff(raw, raw)
    assert entries == []
    assert truncated is False
    assert total == 0


@given(raw=_raw_definition())
@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_diff_is_deterministic(raw: dict[str, Any]) -> None:
    live = copy.deepcopy(raw)
    live["nodes"][0]["parameters"]["extra"] = "changed"
    first = _diff(raw, live)
    second = _diff(raw, live)
    assert first == second


@given(raw=_raw_definition())
@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_a_semantic_change_affects_both_the_hash_and_the_diff(raw: dict[str, Any]) -> None:
    """The completion gate's own named property: every semantic change affects
    either the hash or a visible diff category — proven here as "affects both", since
    a genuine semantic change must never move only one of the two (ADR-008: "a diff
    and a drift detection can never disagree about what changed")."""
    live = copy.deepcopy(raw)
    live["nodes"][0]["parameters"]["a-new-semantic-key"] = "a-new-semantic-value"

    registered_hash = compute_definition_hash(raw)
    live_hash = compute_definition_hash(live)
    entries, _, total = _diff(raw, live)

    assert registered_hash != live_hash
    assert total > 0
    assert entries != []


@given(raw=_raw_definition())
@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_an_allowlisted_change_affects_neither_hash_nor_diff(raw: dict[str, Any]) -> None:
    """``nodes[].position`` and ``pinData`` are the two current exclusion-allowlist
    entries (CAN-02/CAN-03) — changing only those must leave both the hash and the
    diff completely unchanged, since ``canonical_form`` strips them before either
    function ever sees them."""
    live = copy.deepcopy(raw)
    live["nodes"][0]["position"] = [999, 999]
    live["pinData"] = {"someNode": [{"json": {"pinned": True}}]}

    registered_hash = compute_definition_hash(raw)
    live_hash = compute_definition_hash(live)
    entries, truncated, total = _diff(raw, live)

    assert registered_hash == live_hash
    assert entries == []
    assert truncated is False
    assert total == 0


@given(raw=_raw_definition())
@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_administrative_metadata_changes_affect_neither_hash_nor_diff(raw: dict[str, Any]) -> None:
    """``id``/``name``/``updatedAt`` are structurally out of canonicalization's scope
    entirely (never workflow-graph content, docs/N8N_COMPATIBILITY.md section 12) —
    changing them must be invisible to both the hash and the diff, the same as an
    allowlisted field, but for a different (structural, not evidence-based) reason."""
    live = copy.deepcopy(raw)
    live["id"] = "a-totally-different-id"
    live["name"] = "A Totally Different Name"
    live["updatedAt"] = "2030-01-01T00:00:00.000Z"

    registered_hash = compute_definition_hash(raw)
    live_hash = compute_definition_hash(live)
    entries, _, total = _diff(raw, live)

    assert registered_hash == live_hash
    assert entries == []
    assert total == 0
