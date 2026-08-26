"""``core/idempotency.py``: canonicalization, fingerprinting, the argument-size guard,
and idempotency resolution (BUILD_PLAN section 12, ADR-011)."""

from __future__ import annotations

import pytest

from n8n_operator.core.idempotency import (
    IdempotencyNamespace,
    IdempotencyResolution,
    canonicalize_arguments,
    check_argument_size,
    fingerprint_arguments,
    resolve_idempotency,
)
from n8n_operator.errors import ArgumentsTooLargeError, IdempotencyConflictError


@pytest.mark.unit
def test_canonicalize_then_fingerprint_is_deterministic() -> None:
    a = fingerprint_arguments(canonicalize_arguments({"tier": "pro", "email": "a@b.com"}))
    b = fingerprint_arguments(canonicalize_arguments({"email": "a@b.com", "tier": "pro"}))
    assert a == b


@pytest.mark.unit
def test_fingerprint_is_sha256_prefixed() -> None:
    fp = fingerprint_arguments(canonicalize_arguments({"x": 1}))
    assert fp.startswith("sha256:")
    hex_part = fp.removeprefix("sha256:")
    assert len(hex_part) == 64
    int(hex_part, 16)


@pytest.mark.unit
def test_different_arguments_produce_different_fingerprints() -> None:
    a = fingerprint_arguments(canonicalize_arguments({"x": 1}))
    b = fingerprint_arguments(canonicalize_arguments({"x": 2}))
    assert a != b


@pytest.mark.unit
def test_check_argument_size_passes_under_the_limit() -> None:
    check_argument_size(canonicalize_arguments({"x": 1}), effective_limit=1000)  # no raise


@pytest.mark.unit
def test_check_argument_size_passes_exactly_at_the_limit() -> None:
    canonical = canonicalize_arguments({"x": 1})
    check_argument_size(canonical, effective_limit=len(canonical))  # no raise


@pytest.mark.unit
def test_check_argument_size_raises_over_the_limit() -> None:
    canonical = canonicalize_arguments({"x": "y" * 1000})
    with pytest.raises(ArgumentsTooLargeError) as excinfo:
        check_argument_size(canonical, effective_limit=10)
    assert excinfo.value.details["limit"] == 10
    assert excinfo.value.details["size"] == len(canonical)


@pytest.mark.unit
def test_check_argument_size_error_is_not_retryable() -> None:
    canonical = canonicalize_arguments({"x": "y" * 1000})
    with pytest.raises(ArgumentsTooLargeError) as excinfo:
        check_argument_size(canonical, effective_limit=1)
    assert excinfo.value.retryable is False


# --------------------------------------------------------------------------------------
# resolve_idempotency
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_no_prior_operation_resolves_new() -> None:
    result = resolve_idempotency(existing_fingerprint=None, new_fingerprint="sha256:" + "a" * 64)
    assert result is IdempotencyResolution.NEW


@pytest.mark.unit
def test_matching_fingerprint_resolves_replay() -> None:
    fp = "sha256:" + "a" * 64
    result = resolve_idempotency(existing_fingerprint=fp, new_fingerprint=fp)
    assert result is IdempotencyResolution.REPLAY


@pytest.mark.unit
def test_mismatched_fingerprint_raises_conflict() -> None:
    with pytest.raises(IdempotencyConflictError) as excinfo:
        resolve_idempotency(
            existing_fingerprint="sha256:" + "a" * 64, new_fingerprint="sha256:" + "b" * 64
        )
    assert excinfo.value.code == "IDEMPOTENCY_CONFLICT"
    assert excinfo.value.retryable is False


@pytest.mark.unit
def test_idempotency_namespace_carries_all_four_components() -> None:
    ns = IdempotencyNamespace(
        principal_id="local", environment="default", workflow_id="wf.a", idempotency_key="k1"
    )
    assert ns.principal_id == "local"
    assert ns.environment == "default"
    assert ns.workflow_id == "wf.a"
    assert ns.idempotency_key == "k1"


@pytest.mark.unit
def test_idempotency_namespace_is_frozen() -> None:
    ns = IdempotencyNamespace("local", "default", "wf.a", "k1")
    with pytest.raises(AttributeError):
        ns.workflow_id = "wf.b"  # type: ignore[misc]


@pytest.mark.unit
def test_idempotency_namespace_equality_is_by_value() -> None:
    a = IdempotencyNamespace("local", "default", "wf.a", "k1")
    b = IdempotencyNamespace("local", "default", "wf.a", "k1")
    c = IdempotencyNamespace("local", "default", "wf.a", "k2")
    assert a == b
    assert a != c
