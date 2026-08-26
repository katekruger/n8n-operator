"""The error taxonomy: shape, categories, and secret-safety (BUILD_PLAN section 12, phase 1).

Cross-checking every code and remediation string against MCP_TOOLS.md itself lives in
``tests/contract/test_error_taxonomy.py`` — these tests are about the *mechanism*
(``to_dict``, ``from_exception``, the ``_scrub`` safety net, category separation), not
about matching a specific document.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from n8n_operator.errors import (
    TAXONOMY,
    ApprovalRequiredError,
    AuthorizationError,
    ConfigurationError,
    DispatchIndeterminateError,
    DomainError,
    HandleAlreadyUsedError,
    HandleInvalidError,
    InstanceUnreachableError,
    InternalError,
    OperationNotFoundError,
    OperatorError,
    OptimisticLockError,
    ProviderError,
    RegistryUnavailableError,
    StorageError,
    WorkflowNotFoundError,
)

# --------------------------------------------------------------------------------------
# Basic shape: code, message, details, retryable, remediation
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_default_message_is_used_when_none_supplied() -> None:
    error = WorkflowNotFoundError()
    assert error.message == WorkflowNotFoundError.default_message
    assert str(error) == WorkflowNotFoundError.default_message


@pytest.mark.unit
def test_explicit_message_overrides_default() -> None:
    error = WorkflowNotFoundError("custom message for this call site")
    assert error.message == "custom message for this call site"


@pytest.mark.unit
def test_to_dict_has_exactly_the_wire_shape() -> None:
    error = HandleAlreadyUsedError(details={"operation_id": "op_123"})
    payload = error.to_dict()
    assert set(payload.keys()) == {"code", "message", "details", "retryable"}
    assert payload["code"] == "HANDLE_ALREADY_USED"
    assert payload["retryable"] is False
    assert payload["details"] == {"operation_id": "op_123"}


@pytest.mark.unit
def test_details_defaults_to_an_empty_dict_not_shared_between_instances() -> None:
    a = OperationNotFoundError()
    b = OperationNotFoundError()
    a.details["x"] = 1
    assert b.details == {}


@pytest.mark.unit
def test_details_is_copied_not_aliased() -> None:
    original = {"key": "value"}
    error = OperationNotFoundError(details=original)
    error.details["key"] = "mutated"
    assert original["key"] == "value"


@pytest.mark.unit
def test_repr_includes_code_and_message_not_details() -> None:
    error = WorkflowNotFoundError("wf.missing", details={"secret": "should-not-appear-here"})
    r = repr(error)
    assert "WORKFLOW_NOT_FOUND" in r
    assert "wf.missing" in r
    assert "should-not-appear-here" not in r


# --------------------------------------------------------------------------------------
# retryable — pinned exactly for the two the doc calls out explicitly, plus the general
# rule that nothing side-effect-adjacent on execute is retryable=True.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_already_used_and_dispatch_indeterminate_are_not_retryable() -> None:
    # MCP_TOOLS.md section 4.1 calls these two out by name.
    assert HandleAlreadyUsedError.retryable is False
    assert DispatchIndeterminateError.retryable is False


@pytest.mark.unit
def test_only_transient_provider_and_throttling_codes_are_retryable() -> None:
    retryable_codes = {code for code, cls in TAXONOMY.items() if cls.retryable}
    assert retryable_codes == {"INSTANCE_UNREACHABLE", "RATE_LIMITED", "CONCURRENCY_LIMIT_REACHED"}


@pytest.mark.unit
def test_approval_required_is_not_retryable() -> None:
    # A side-effect-adjacent gate on execute_operation; retrying in a loop is exactly
    # the behavior ADR-005/P6 exist to prevent.
    assert ApprovalRequiredError.retryable is False


# --------------------------------------------------------------------------------------
# Category hierarchy — domain / authorization / provider / configuration / storage
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_five_categories_are_distinct_and_all_derive_from_operator_error() -> None:
    categories = [DomainError, AuthorizationError, ProviderError, ConfigurationError, StorageError]
    for category in categories:
        assert issubclass(category, OperatorError)
    # Pairwise distinct: no category is a subclass of another.
    for i, a in enumerate(categories):
        for b in categories[i + 1 :]:
            assert not issubclass(a, b)
            assert not issubclass(b, a)


@pytest.mark.unit
def test_authorization_errors_are_capability_failures() -> None:
    for cls in (ApprovalRequiredError, HandleInvalidError, HandleAlreadyUsedError):
        assert issubclass(cls, AuthorizationError)


@pytest.mark.unit
def test_provider_errors_are_n8n_facing() -> None:
    assert issubclass(InstanceUnreachableError, ProviderError)
    assert issubclass(DispatchIndeterminateError, ProviderError)


@pytest.mark.unit
def test_registry_unavailable_is_a_configuration_error() -> None:
    assert issubclass(RegistryUnavailableError, ConfigurationError)
    assert RegistryUnavailableError.code == "REGISTRY_UNAVAILABLE"


@pytest.mark.unit
def test_storage_error_and_internal_error_both_default_to_internal_error_code() -> None:
    # The taxonomy has no dedicated storage-facing wire code; both share INTERNAL_ERROR
    # while remaining distinct Python exception types for `except` clauses to target.
    assert StorageError.code == "INTERNAL_ERROR"
    assert InternalError.code == "INTERNAL_ERROR"
    assert not issubclass(StorageError, InternalError)
    assert not issubclass(InternalError, StorageError)


@pytest.mark.unit
def test_optimistic_lock_error_is_a_storage_error() -> None:
    assert issubclass(OptimisticLockError, StorageError)


# --------------------------------------------------------------------------------------
# from_exception — never copies the wrapped exception's own text.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_from_exception_does_not_copy_the_wrapped_message() -> None:
    underlying = RuntimeError("connection string: postgresql://user:hunter2@host/db")
    wrapped = StorageError.from_exception(underlying, message="a database operation failed")
    assert wrapped.message == "a database operation failed"
    assert "hunter2" not in wrapped.message
    assert "hunter2" not in repr(wrapped.to_dict())


@pytest.mark.unit
def test_from_exception_sets_cause_for_local_debugging() -> None:
    underlying = ValueError("boom")
    wrapped = StorageError.from_exception(underlying, message="safe message")
    assert wrapped.__cause__ is underlying


@pytest.mark.unit
def test_from_exception_details_are_only_what_the_caller_explicitly_passed() -> None:
    underlying = RuntimeError("some driver-internal detail nobody asked to expose")
    wrapped = StorageError.from_exception(
        underlying, message="failed", details={"table": "operations"}
    )
    assert wrapped.details == {"table": "operations"}
    assert "driver-internal" not in repr(wrapped.to_dict())


# --------------------------------------------------------------------------------------
# _scrub — the defensive net against an accidentally-included secret in `details`.
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_to_dict_scrubs_a_secretstr_accidentally_placed_in_details() -> None:
    error = InternalError(
        "something failed", details={"accidentally_included": SecretStr("do-not-leak-me")}
    )
    payload = error.to_dict()
    assert "do-not-leak-me" not in repr(payload)
    assert payload["details"]["accidentally_included"] == "[REDACTED]"


@pytest.mark.unit
def test_to_dict_scrubs_secrets_nested_in_lists_and_dicts() -> None:
    error = InternalError(
        "failed",
        details={
            "nested": {"inner_secret": SecretStr("nested-secret-value")},
            "a_list": [SecretStr("listed-secret-value"), "plain-string"],
        },
    )
    payload = error.to_dict()
    assert "nested-secret-value" not in repr(payload)
    assert "listed-secret-value" not in repr(payload)
    assert payload["details"]["a_list"][1] == "plain-string"


@pytest.mark.unit
def test_to_dict_output_is_json_serializable_even_with_a_scrubbed_secret() -> None:
    import json

    error = InternalError("failed", details={"secret": SecretStr("value")})
    # Must not raise — this is what proves the scrub actually replaces the object rather
    # than merely relying on SecretStr's own __str__ masking (which json.dumps ignores).
    serialized = json.dumps(error.to_dict())
    assert "value" not in serialized


@pytest.mark.unit
def test_scrub_does_not_alter_ordinary_details() -> None:
    error = InternalError("failed", details={"operation_id": "op_123", "count": 3, "ok": True})
    assert error.to_dict()["details"] == {"operation_id": "op_123", "count": 3, "ok": True}


# --------------------------------------------------------------------------------------
# The taxonomy registry itself
# --------------------------------------------------------------------------------------


@pytest.mark.unit
def test_taxonomy_has_exactly_24_codes() -> None:
    assert len(TAXONOMY) == 24


@pytest.mark.unit
def test_taxonomy_keys_match_each_class_own_code() -> None:
    for code, cls in TAXONOMY.items():
        assert cls.code == code


@pytest.mark.unit
def test_every_taxonomy_class_is_an_operator_error_with_full_shape() -> None:
    for cls in TAXONOMY.values():
        assert issubclass(cls, OperatorError)
        instance = cls()
        assert isinstance(instance.code, str) and instance.code
        assert isinstance(instance.remediation, str) and instance.remediation
        assert isinstance(instance.retryable, bool)


@pytest.mark.unit
def test_operator_error_is_a_real_exception() -> None:
    with pytest.raises(WorkflowNotFoundError):
        raise WorkflowNotFoundError("wf.x")


@pytest.mark.unit
def test_catching_the_category_catches_every_member() -> None:
    with pytest.raises(AuthorizationError):
        raise HandleInvalidError()
    with pytest.raises(DomainError):
        raise WorkflowNotFoundError()
    with pytest.raises(ProviderError):
        raise InstanceUnreachableError()


@pytest.mark.unit
def test_catching_operator_error_catches_everything() -> None:
    for cls in TAXONOMY.values():
        with pytest.raises(OperatorError):
            raise cls()


@pytest.mark.unit
def test_taxonomy_values_are_distinct_classes() -> None:
    classes: list[type[Any]] = list(TAXONOMY.values())
    assert len(classes) == len(set(classes))
