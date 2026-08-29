"""Hypothesis property tests for the registry (BUILD_PLAN section 10.2, phase 2).

Ten properties, as specified for phase 2:

1. Hash stability under YAML key ordering and whitespace differences.
2. Hash changes for semantic registry changes.
3. Round-trip parsing.
4. Literal secrets are always rejected (R6).
5. Absolute webhook URLs are always rejected (R8).
6. ``approval: none`` can only coexist with ``read_only`` (R5).
7. High risk always requires approval (R10).
8. Oversized canonical arguments fail (ADR-011).
9. Same scoped idempotency key + same fingerprint resolves consistently.
10. Same scoped key + different fingerprint conflicts.

Properties 4-7 exercise the individual rule-check functions directly, via
``model_construct`` (bypassing Pydantic validation), rather than round-tripping through
YAML + the full document schema: what each of these properties claims is a fact about
the *rule*, independent of every other rule that a full registry document would also
have to satisfy simultaneously.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from n8n_operator.core.idempotency import (
    IdempotencyResolution,
    check_argument_size,
    resolve_idempotency,
)
from n8n_operator.errors import ArgumentsTooLargeError, IdempotencyConflictError
from n8n_operator.registry.loader import (
    LoadedRegistry,
    _check_r5_r10_r15_approval_policy,
    _check_r6_secret_ref,
    _check_r8_trigger_path,
    load_registry,
    parse_registry_yaml,
)
from n8n_operator.registry.schema import Trigger, WorkflowEntry

# --------------------------------------------------------------------------------------
# Shared fixtures / helpers
# --------------------------------------------------------------------------------------

SAFE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

BASE_ENTRY: dict[str, Any] = {
    "id": "wf.a",
    "n8n_workflow_id": "n8n-1",
    "title": "A workflow",
    "description": "Does a thing.",
    "owner": "carolyn",
    "version": 1,
    "definition_hash": "sha256:" + "a" * 64,
    "risk": "low",
    "side_effects": "read_only",
    "approval": "none",
    "trigger": {"type": "webhook", "method": "POST", "path": "/webhook/a", "auth": "none"},
    "input_schema": {"type": "object", "additionalProperties": False},
}

BASE_DOC: dict[str, Any] = {
    "apiVersion": "n8n-operator/v1",
    "metadata": {"name": "prop-test"},
    "workflows": [BASE_ENTRY],
}


def _load_text(text: str) -> LoadedRegistry:
    fd, path_str = tempfile.mkstemp(suffix=".yaml")
    path = Path(path_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return load_registry(path, server_max_argument_bytes=262_144)
    finally:
        path.unlink()


def _load_doc(doc: dict[str, Any]) -> LoadedRegistry:
    return _load_text(yaml.dump(doc, sort_keys=False, default_flow_style=False))


# --------------------------------------------------------------------------------------
# 1. Hash stability under YAML key ordering and whitespace differences
# --------------------------------------------------------------------------------------

TOP_KEYS = ["apiVersion", "metadata", "workflows"]
ENTRY_KEYS = list(BASE_ENTRY.keys())
TRIGGER_KEYS = list(BASE_ENTRY["trigger"].keys())


@given(
    top_order=st.permutations(TOP_KEYS),
    entry_order=st.permutations(ENTRY_KEYS),
    trigger_order=st.permutations(TRIGGER_KEYS),
    leading_blank_lines=st.integers(min_value=0, max_value=5),
    trailing_blank_lines=st.integers(min_value=0, max_value=5),
    comment_lines=st.integers(min_value=0, max_value=3),
)
@settings(max_examples=25, deadline=None)
def test_hash_stable_under_key_order_and_whitespace(
    top_order: list[str],
    entry_order: list[str],
    trigger_order: list[str],
    leading_blank_lines: int,
    trailing_blank_lines: int,
    comment_lines: int,
) -> None:
    baseline = _load_doc(BASE_DOC)

    reordered_trigger = {k: BASE_ENTRY["trigger"][k] for k in trigger_order}
    reordered_entry: dict[str, Any] = {
        **{k: BASE_ENTRY[k] for k in entry_order},
        "trigger": reordered_trigger,
    }
    reordered_doc = {k: (BASE_DOC[k] if k != "workflows" else [reordered_entry]) for k in top_order}

    body = yaml.dump(reordered_doc, sort_keys=False, default_flow_style=False)
    prefix = "\n".join(["# a comment"] * comment_lines + [""] * leading_blank_lines)
    suffix = "\n" * trailing_blank_lines
    text = (prefix + "\n" if prefix else "") + body + suffix

    variant = _load_text(text)
    assert variant.content_hash == baseline.content_hash


# --------------------------------------------------------------------------------------
# 2. Hash changes for semantic registry changes
# --------------------------------------------------------------------------------------


@given(new_title=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=20))
@settings(max_examples=25, deadline=None)
def test_hash_changes_for_a_semantic_change(new_title: str) -> None:
    assume(new_title != BASE_ENTRY["title"])
    baseline = _load_doc(BASE_DOC)

    mutated_doc = {
        **BASE_DOC,
        "workflows": [{**BASE_ENTRY, "title": new_title}],
    }
    mutated = _load_doc(mutated_doc)

    assert mutated.content_hash != baseline.content_hash


# --------------------------------------------------------------------------------------
# 3. Round-trip parsing
# --------------------------------------------------------------------------------------

_json_leaf = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-10_000, max_value=10_000)
    | st.text(alphabet=SAFE_ALPHABET, max_size=10)
)
_json_safe = st.recursive(
    _json_leaf,
    lambda children: (
        st.lists(children, max_size=5)
        | st.dictionaries(
            st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=10), children, max_size=5
        )
    ),
    max_leaves=20,
)


@given(value=_json_safe)
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_yaml_round_trip_preserves_the_value(value: Any) -> None:
    text = yaml.safe_dump(value)
    result = parse_registry_yaml(text)
    assert result == value


# --------------------------------------------------------------------------------------
# 4. Literal secrets are always rejected (R6)
# --------------------------------------------------------------------------------------


@given(
    secret=st.text(min_size=1, max_size=30).filter(
        lambda s: not (s.startswith("env:") or s.startswith("keyring:"))
    )
)
@settings(max_examples=50, deadline=None)
def test_literal_secrets_are_always_rejected(secret: str) -> None:
    trigger = Trigger.model_construct(
        type="webhook",
        method="POST",
        path="/webhook/x",
        auth="header",
        secret_ref=secret,
        correlation="none",
    )
    entry = WorkflowEntry.model_construct(id="wf.x", trigger=trigger)
    violation = _check_r6_secret_ref(entry)
    assert violation is not None
    assert violation.rule == "R6"


# --------------------------------------------------------------------------------------
# 5. Absolute webhook URLs are always rejected (R8)
# --------------------------------------------------------------------------------------


@given(
    scheme=st.sampled_from(["http", "https", "ftp", "ws", "wss"]),
    host=st.text(alphabet=SAFE_ALPHABET, min_size=1, max_size=12),
    rest=st.text(alphabet=SAFE_ALPHABET, max_size=10),
)
@settings(max_examples=50, deadline=None)
def test_absolute_webhook_urls_are_always_rejected(scheme: str, host: str, rest: str) -> None:
    path = f"{scheme}://{host}/{rest}"
    entry = WorkflowEntry.model_construct(
        id="wf.x",
        trigger=Trigger.model_construct(type="webhook", method="POST", path=path, auth="none"),
    )
    violation = _check_r8_trigger_path(entry)
    assert violation is not None
    assert violation.rule == "R8"


# --------------------------------------------------------------------------------------
# 6. approval: none can only coexist with read_only (R5)
# --------------------------------------------------------------------------------------


@given(
    approval=st.sampled_from(["none", "required"]),
    side_effects=st.sampled_from(["read_only", "external_write", "irreversible"]),
    risk=st.sampled_from(["low", "medium", "high"]),
)
@settings(max_examples=50, deadline=None)
def test_approval_none_can_only_coexist_with_read_only(
    approval: str, side_effects: str, risk: str
) -> None:
    entry = WorkflowEntry.model_construct(
        id="wf.x", approval=approval, side_effects=side_effects, risk=risk
    )
    r5_violations = [v for v in _check_r5_r10_r15_approval_policy(entry) if v.rule == "R5"]
    if approval == "none" and side_effects != "read_only":
        assert len(r5_violations) == 1
    else:
        assert r5_violations == []


# --------------------------------------------------------------------------------------
# 7. High risk always requires approval (R10)
# --------------------------------------------------------------------------------------


@given(
    risk=st.sampled_from(["low", "medium", "high"]),
    approval=st.sampled_from(["none", "required"]),
    side_effects=st.sampled_from(["read_only", "external_write", "irreversible"]),
)
@settings(max_examples=50, deadline=None)
def test_high_risk_always_requires_approval(risk: str, approval: str, side_effects: str) -> None:
    entry = WorkflowEntry.model_construct(
        id="wf.x", approval=approval, side_effects=side_effects, risk=risk
    )
    r10_violations = [v for v in _check_r5_r10_r15_approval_policy(entry) if v.rule == "R10"]
    if risk == "high" and approval != "required":
        assert len(r10_violations) == 1
    else:
        assert r10_violations == []


# --------------------------------------------------------------------------------------
# 8. Oversized canonical arguments fail (ADR-011)
# --------------------------------------------------------------------------------------


@given(
    limit=st.integers(min_value=1, max_value=1000), size=st.integers(min_value=0, max_value=2000)
)
@settings(max_examples=50, deadline=None)
def test_oversized_canonical_arguments_fail(limit: int, size: int) -> None:
    data = b"x" * size
    if size > limit:
        with pytest.raises(ArgumentsTooLargeError):
            check_argument_size(data, effective_limit=limit)
    else:
        check_argument_size(data, effective_limit=limit)  # must not raise


# --------------------------------------------------------------------------------------
# 9. Same scoped idempotency key + same fingerprint resolves consistently
# --------------------------------------------------------------------------------------


@given(fingerprint=st.text(min_size=1, max_size=20))
@settings(max_examples=50, deadline=None)
def test_same_key_same_fingerprint_resolves_consistently(fingerprint: str) -> None:
    first = resolve_idempotency(existing_fingerprint=None, new_fingerprint=fingerprint)
    assert first is IdempotencyResolution.NEW

    second = resolve_idempotency(existing_fingerprint=fingerprint, new_fingerprint=fingerprint)
    third = resolve_idempotency(existing_fingerprint=fingerprint, new_fingerprint=fingerprint)
    assert second is IdempotencyResolution.REPLAY
    assert second is third


# --------------------------------------------------------------------------------------
# 10. Same scoped key + different fingerprint conflicts
# --------------------------------------------------------------------------------------


@given(existing=st.text(min_size=1, max_size=20), new=st.text(min_size=1, max_size=20))
@settings(max_examples=50, deadline=None)
def test_different_fingerprint_always_conflicts(existing: str, new: str) -> None:
    assume(existing != new)
    with pytest.raises(IdempotencyConflictError):
        resolve_idempotency(existing_fingerprint=existing, new_fingerprint=new)
