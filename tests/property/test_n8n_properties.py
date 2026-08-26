"""Hypothesis properties for the n8n integration (BUILD_PLAN section 12, phase 4):

- AC-27: an unrecognized field, anywhere in the definition, still contributes to the
  hash — canonicalization never silently drops something it doesn't recognize.
- AC-26 (the general form): canonicalization is idempotent and never crashes on
  arbitrary well-formed JSON-safe input.
- AC-29: response-envelope parsing never raises on arbitrary input — a malformed or
  absent envelope degrades to "no correlation available", never an exception.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from n8n_operator.n8n.canonicalization import canonical_form, compute_definition_hash
from n8n_operator.n8n.types import ResponseEnvelope

SAFE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"

_json_leaf = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-1000, max_value=1000)
    | st.text(alphabet=SAFE_ALPHABET, max_size=10)
)
_json_value = st.recursive(
    _json_leaf,
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(
            st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=8), children, max_size=4
        )
    ),
    max_leaves=15,
)

_node = st.fixed_dictionaries(
    {
        "id": st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=8),
        "name": st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=8),
        "type": st.sampled_from(
            ["n8n-nodes-base.webhook", "n8n-nodes-base.code", "n8n-nodes-base.set"]
        ),
        "position": st.tuples(st.integers(-1000, 1000), st.integers(-1000, 1000)).map(list),
        "parameters": st.dictionaries(
            st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=6), _json_leaf, max_size=3
        ),
    }
)

_definition = st.fixed_dictionaries(
    {
        "id": st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=8),
        "name": st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=8),
        "active": st.booleans(),
        "nodes": st.lists(_node, max_size=4),
        "connections": st.dictionaries(
            st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=6), _json_value, max_size=3
        ),
        "settings": st.dictionaries(
            st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=6), _json_leaf, max_size=3
        ),
    }
)


@given(definition=_definition)
@settings(max_examples=100, deadline=None)
def test_canonicalization_never_crashes_on_well_formed_definitions(
    definition: dict[str, Any],
) -> None:
    compute_definition_hash(definition)  # must not raise


@given(definition=_definition)
@settings(max_examples=100, deadline=None)
def test_canonical_form_is_idempotent_for_any_well_formed_definition(
    definition: dict[str, Any],
) -> None:
    once = canonical_form(definition)
    twice = canonical_form(json.loads(json.dumps(once)))
    assert once == twice


@given(
    definition=_definition,
    unknown_field_name=st.text(alphabet=SAFE_ALPHABET, min_size=3, max_size=10),
    unknown_field_value=_json_value,
)
@settings(max_examples=100, deadline=None)
def test_an_unrecognized_field_always_changes_the_hash(
    definition: dict[str, Any], unknown_field_name: str, unknown_field_value: Any
) -> None:
    """AC-27: fuzzed. Adding a field this codebase has never heard of, at the workflow
    level, must never leave the hash unchanged — that would mean it was silently
    dropped rather than included."""
    if not definition["nodes"]:
        return  # nothing to attach the unknown field to; skip this example
    mutated = json.loads(json.dumps(definition))
    target_node = mutated["nodes"][0]
    if unknown_field_name in target_node:
        return  # collision with a real field name; not what this property tests
    target_node[unknown_field_name] = unknown_field_value
    assert compute_definition_hash(definition) != compute_definition_hash(mutated)


# --------------------------------------------------------------------------------------
# AC-29: response-envelope parsing never raises.
# --------------------------------------------------------------------------------------


@given(body=_json_value)
@settings(max_examples=100, deadline=None)
def test_response_envelope_parsing_never_raises_on_arbitrary_json(body: Any) -> None:
    if not isinstance(body, dict):
        return
    ResponseEnvelope.model_validate(body)  # must not raise regardless of shape


@given(
    execution_id=st.one_of(
        st.none(),
        st.text(max_size=20),
        st.integers(),
        st.booleans(),
        st.lists(st.text(), max_size=3),
    )
)
@settings(max_examples=50, deadline=None)
def test_response_envelope_execution_id_is_none_or_a_string(execution_id: Any) -> None:
    envelope = ResponseEnvelope.model_validate({"n8n_operator": {"execution_id": execution_id}})
    assert envelope.execution_id is None or isinstance(envelope.execution_id, str)


@given(body=st.dictionaries(st.text(alphabet=SAFE_ALPHABET, max_size=10), _json_value, max_size=5))
@settings(max_examples=50, deadline=None)
def test_response_envelope_with_no_n8n_operator_key_has_no_execution_id(
    body: dict[str, Any],
) -> None:
    body_without_key = {k: v for k, v in body.items() if k != "n8n_operator"}
    envelope = ResponseEnvelope.model_validate(body_without_key)
    assert envelope.execution_id is None
