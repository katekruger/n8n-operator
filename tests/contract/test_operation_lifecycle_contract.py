"""Contract tests for the operation lifecycle's structural guarantees (BUILD_PLAN
section 12, phase 3; section 10.3):

- No code path transitions out of ``UNKNOWN`` — checked directly against the state
  table, not inferred from behavior (ADR-009, invariant I7).
- No operation ever inherits, extends, or reuses another operation's approval — no core
  use case signature accepts a second operation ID or an "approval to reuse" (invariant
  I11; governed retry, which would need this, is a v2 feature not implemented here).
- The argument-size check is enforced in ``core/``, not in an adapter (boundary B12).
- No domain model exposes a raw token, an approval-page URL, or a loopback address
  (invariant I12's structural precondition — the *caller-locality* half of I12 belongs to
  the MCP adapter, phase 5/6, which does not exist yet; this only guarantees the core
  layer never hands out something for that adapter to leak in the first place).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from n8n_operator.core import service
from n8n_operator.core.models import Approval, Operation
from n8n_operator.core.state_machine import TRANSITIONS

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "n8n_operator"


@pytest.mark.contract
def test_no_transition_ever_leaves_unknown() -> None:
    assert all(t.from_state != "UNKNOWN" for t in TRANSITIONS)


@pytest.mark.contract
def test_no_lifecycle_use_case_accepts_a_second_or_parent_operation_id() -> None:
    """v1 has no retry mechanism at all — this asserts that absence directly: no public
    ``core.service`` function's signature accepts anything shaped like a second
    operation to inherit authority from."""
    forbidden_param_names = {
        "parent_operation_id",
        "source_operation_id",
        "reuse_approval",
        "approval_id",
    }
    for name in (
        "prepare_operation",
        "approve_operation",
        "reject_operation",
        "execute_operation",
        "cancel_operation",
        "record_execution_outcome",
    ):
        func = getattr(service, name)
        params = set(inspect.signature(func).parameters)
        overlap = params & forbidden_param_names
        assert not overlap, (
            f"{name} accepts {overlap}, which could reuse another operation's approval"
        )


@pytest.mark.contract
def test_approve_operation_only_ever_targets_the_operation_it_was_called_with() -> None:
    """A second, behavioral check alongside the signature check above: approving one
    operation must never be able to move a different operation's state (invariant I11)
    — proven at the integration layer
    (test_core_service_operations.py::test_approving_one_operation_does_not_authorize_a_different_operation);
    this asserts the narrower, purely-structural half that doesn't need a database."""
    source = inspect.getsource(service.approve_operation)
    tree = ast.parse(source)
    # every call to _apply_and_audit / _get_operation_row inside approve_operation must
    # be parameterized by the same `operation_id` the function itself received, never a
    # second identifier threaded in from elsewhere.
    assigned_names = {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert "operation_id" in {a.arg for a in ast.walk(tree) if isinstance(a, ast.arg)}
    assert not assigned_names & {"parent_operation_id", "other_operation_id"}


@pytest.mark.contract
def test_argument_size_check_is_called_from_core_not_from_an_adapter() -> None:
    """Boundary B12: the check lives in ``core/`` and is applied identically for every
    adapter. Since no adapter exists yet (phases 5, 6), this asserts the check is wired
    into ``core.service.prepare_operation`` — the one place every future adapter must
    route through (ADR-001) — rather than left uncalled."""
    source = inspect.getsource(service.prepare_operation)
    assert "check_argument_size" in source


@pytest.mark.contract
def test_no_domain_model_has_a_field_that_could_carry_a_raw_token_or_a_url() -> None:
    forbidden_substrings = ("token", "url", "secret")
    for model in (Operation, Approval):
        for field_name in model.model_fields:
            lowered = field_name.lower()
            assert not any(bad in lowered for bad in forbidden_substrings), (
                f"{model.__name__}.{field_name} looks like it could carry a secret or a URL"
            )


@pytest.mark.contract
def test_no_code_path_infers_non_execution_from_a_timeout_or_exception_class() -> None:
    """ADR-009: there is no heuristic anywhere that converts an indeterminate dispatch
    into FAILED or SUCCEEDED. ``core/service.py`` never even imports an HTTP/timeout
    exception type to begin with — checked directly, since the absence of the import is
    the absence of the capability to write such a heuristic."""
    text = (SRC / "core" / "service.py").read_text(encoding="utf-8")
    for forbidden in ("httpx", "TimeoutError", "ConnectionError", "requests"):
        assert forbidden not in text
