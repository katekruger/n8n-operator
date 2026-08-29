"""``core.service.retry_operation`` (stage 06, ADR-005/ADR-009/ADR-012, invariant I11)
against a real database — the eligible/ineligible parent-state matrix, chain-depth
refusal, drift/reclassification/disablement/environment-removal recalculation, and
idempotency scoping (including the concurrent-retry race's non-concurrent half; the
genuinely concurrent half is ``tests/integration/postgres/test_retry_concurrency.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import PreflightCheck, PreflightResult
from n8n_operator.errors import (
    EnvironmentArchivedError,
    OperationNotFoundError,
    RetryNotApplicableError,
    WorkflowDisabledError,
)
from n8n_operator.storage.models import Operation as OperationRow
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: retry-test
workflows:
  - id: wf.a
    n8n_workflow_id: n8n-1
    title: Needs approval
    description: External write.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: medium
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      properties:
        note: {{type: string}}
      required: [note]
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
  - id: wf.b
    n8n_workflow_id: n8n-2
    title: Auto-approved
    description: Read-only.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_b}
    risk: low
    side_effects: read_only
    approval: none
    trigger:
      type: webhook
      method: POST
      path: /webhook/b
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
""".format(hash_a="a" * 64, hash_b="b" * 64)


class FakePreflight:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready

    def check(self, workflow: Any) -> PreflightResult:
        checks = [] if self.ready else [PreflightCheck(check="drift", status="fail")]
        return PreflightResult(ready=self.ready, checks=checks, checked_at=datetime.now(UTC))


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def env(session_factory: sessionmaker[Session], registry_path: Path) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
    return {"principal_id": "local", "registry_path": registry_path}


def _default_arguments(workflow_id: str) -> dict[str, Any]:
    return {"note": "x"} if workflow_id == "wf.a" else {}


def _prepare(
    session_factory: sessionmaker[Session],
    *,
    workflow_id: str = "wf.a",
    arguments: dict[str, Any] | None = None,
    preflight: Any = None,
    principal_id: str = "local",
) -> str:
    with session_scope(session_factory) as session:
        operation, _, _ = service.prepare_operation(
            session,
            principal_id=principal_id,
            environment="default",
            workflow_id=workflow_id,
            arguments=arguments if arguments is not None else _default_arguments(workflow_id),
            preflight=preflight or FakePreflight(),
            server_max_argument_bytes=262_144,
        )
        return operation.id


def _to_executing(session_factory: sessionmaker[Session], operation_id: str) -> None:
    with session_scope(session_factory) as session:
        service.execute_operation(
            session,
            operation_id=operation_id,
            handle=operation_id,
            principal_id="local",
            preflight=FakePreflight(),
        )


def _make_failed(session_factory: sessionmaker[Session]) -> str:
    op_id = _prepare(session_factory, workflow_id="wf.b")  # approval: none -> APPROVED
    _to_executing(session_factory, op_id)
    with session_scope(session_factory) as session:
        service.record_execution_outcome(
            session, operation_id=op_id, outcome="error", error={"message": "boom"}
        )
    return op_id


def _make_unknown(session_factory: sessionmaker[Session]) -> str:
    op_id = _prepare(session_factory, workflow_id="wf.b")
    _to_executing(session_factory, op_id)
    with session_scope(session_factory) as session:
        service.record_execution_outcome(session, operation_id=op_id, outcome="indeterminate")
    return op_id


def _make_succeeded(session_factory: sessionmaker[Session]) -> str:
    op_id = _prepare(session_factory, workflow_id="wf.b")
    _to_executing(session_factory, op_id)
    with session_scope(session_factory) as session:
        service.record_execution_outcome(session, operation_id=op_id, outcome="success")
    return op_id


def _make_blocked(session_factory: sessionmaker[Session]) -> str:
    return _prepare(session_factory, workflow_id="wf.b", preflight=FakePreflight(ready=False))


def _make_rejected(session_factory: sessionmaker[Session]) -> str:
    op_id = _prepare(session_factory, workflow_id="wf.a")
    with session_scope(session_factory) as session:
        service.reject_operation(session, operation_id=op_id, decided_by="local")
    return op_id


def _make_expired(session_factory: sessionmaker[Session]) -> str:
    op_id = _prepare(session_factory, workflow_id="wf.a")
    with session_scope(session_factory) as session:
        session.execute(
            update(OperationRow)
            .where(OperationRow.id == op_id)
            .values(approval_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    return op_id


def _make_canceled(session_factory: sessionmaker[Session]) -> str:
    op_id = _prepare(session_factory, workflow_id="wf.a")
    with session_scope(session_factory) as session:
        service.cancel_operation(session, operation_id=op_id, principal_id="local")
    return op_id


def _make_invalid(session_factory: sessionmaker[Session]) -> str:
    # `wf.a` requires `note`; omitting it fails schema validation -> INVALID (T02).
    return _prepare(session_factory, workflow_id="wf.a", arguments={})


def _make_pending_approval(session_factory: sessionmaker[Session]) -> str:
    return _prepare(session_factory, workflow_id="wf.a")


def _make_approved(session_factory: sessionmaker[Session]) -> str:
    return _prepare(session_factory, workflow_id="wf.b")


def _make_executing(session_factory: sessionmaker[Session]) -> str:
    op_id = _prepare(session_factory, workflow_id="wf.b")
    _to_executing(session_factory, op_id)
    return op_id


def _retry(
    session_factory: sessionmaker[Session],
    operation_id: str,
    *,
    principal_id: str = "local",
    idempotency_key: str | None = None,
    preflight: Any = None,
) -> tuple[Any, bool, str | None]:
    with session_scope(session_factory) as session:
        return service.retry_operation(
            session,
            operation_id=operation_id,
            principal_id=principal_id,
            preflight=preflight or FakePreflight(),
            server_max_argument_bytes=262_144,
            idempotency_key=idempotency_key,
        )


@pytest.mark.parametrize(
    "make_parent",
    [_make_failed, _make_unknown, _make_blocked, _make_rejected, _make_expired],
)
def test_every_retryable_parent_state_succeeds(
    session_factory: sessionmaker[Session], env: dict[str, Any], make_parent: Any
) -> None:
    parent_id = make_parent(session_factory)
    operation, replay, _token = _retry(session_factory, parent_id)
    assert replay is False
    assert operation.parent_operation_id == parent_id
    assert operation.id != parent_id


@pytest.mark.parametrize(
    "make_parent",
    [
        _make_succeeded,
        _make_canceled,
        _make_invalid,
        _make_executing,
        _make_pending_approval,
        _make_approved,
    ],
)
def test_every_non_retryable_parent_state_refuses(
    session_factory: sessionmaker[Session], env: dict[str, Any], make_parent: Any
) -> None:
    parent_id = make_parent(session_factory)
    with pytest.raises(RetryNotApplicableError):
        _retry(session_factory, parent_id)


def test_read_only_retry_reaches_approved_fresh_not_reused(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    parent_id = _make_failed(session_factory)  # wf.b, approval: none
    operation, _, _ = _retry(session_factory, parent_id)
    assert operation.state == "APPROVED"


def test_retry_lands_pending_approval_when_approval_was_tightened_since_parent(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    parent_id = _make_failed(session_factory)  # wf.b: approval none at prepare time
    tightened = REGISTRY_YAML.replace("approval: none", "approval: required")
    assert tightened != REGISTRY_YAML
    with session_scope(session_factory) as session:
        path = env["registry_path"]
        path.write_text(tightened)
        service.reload_registry(session, path, server_max_argument_bytes=262_144)

    operation, _, _ = _retry(session_factory, parent_id)
    assert operation.state == "PENDING_APPROVAL"


def test_retry_after_definition_drift_is_blocked(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    parent_id = _make_failed(session_factory)
    operation, _, _ = _retry(session_factory, parent_id, preflight=FakePreflight(ready=False))
    assert operation.state == "BLOCKED"


def test_retry_after_workflow_disabled_since_parent_refuses(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    parent_id = _make_failed(session_factory)
    disabled = REGISTRY_YAML.replace(
        "  - id: wf.b\n    n8n_workflow_id: n8n-2",
        "  - id: wf.b\n    enabled: false\n    n8n_workflow_id: n8n-2",
    )
    assert disabled != REGISTRY_YAML
    with session_scope(session_factory) as session:
        path = env["registry_path"]
        path.write_text(disabled)
        service.reload_registry(session, path, server_max_argument_bytes=262_144)

    with pytest.raises(WorkflowDisabledError):
        _retry(session_factory, parent_id)


def test_retry_of_a_cross_organization_or_unowned_parent_is_not_found(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    parent_id = _make_failed(session_factory)
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="someone-else", kind="local", display_name="other")
    with pytest.raises(OperationNotFoundError):
        _retry(session_factory, parent_id, principal_id="someone-else")


def test_chain_depth_is_refused_at_the_limit(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    operation_id = _make_failed(session_factory)
    # Each retry adds one ancestor to the next child's chain; after
    # MAX_RETRY_CHAIN_DEPTH successful retries the newest operation has exactly that
    # many ancestors, and the next retry attempt is the one that must refuse.
    for _ in range(service.MAX_RETRY_CHAIN_DEPTH):
        operation, _, _ = _retry(session_factory, operation_id)
        assert operation.state == "APPROVED"
        with session_scope(session_factory) as session:
            service.execute_operation(
                session,
                operation_id=operation.id,
                handle=operation.id,
                principal_id="local",
                preflight=FakePreflight(),
            )
            service.record_execution_outcome(
                session, operation_id=operation.id, outcome="error", error={"message": "boom"}
            )
        operation_id = operation.id
    with pytest.raises(RetryNotApplicableError):
        _retry(session_factory, operation_id)


def test_repeated_idempotency_key_against_the_same_parent_replays(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    parent_id = _make_failed(session_factory)
    first, replay1, _ = _retry(session_factory, parent_id, idempotency_key="retry-key")
    second, replay2, _ = _retry(session_factory, parent_id, idempotency_key="retry-key")
    assert replay1 is False
    assert replay2 is True
    assert first.id == second.id


def test_same_key_against_two_different_parents_does_not_collide(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    parent_a = _make_failed(session_factory)
    parent_b = _make_failed(session_factory)
    first, _, _ = _retry(session_factory, parent_a, idempotency_key="shared-key")
    second, _, _ = _retry(session_factory, parent_b, idempotency_key="shared-key")
    assert first.id != second.id
    assert first.parent_operation_id == parent_a
    assert second.parent_operation_id == parent_b


def test_same_key_as_an_unrelated_prepare_operation_call_does_not_collide(
    session_factory: sessionmaker[Session], env: dict[str, Any]
) -> None:
    parent_id = _make_failed(session_factory)
    with session_scope(session_factory) as session:
        prepared, prepared_replay, _ = service.prepare_operation(
            session,
            principal_id="local",
            environment="default",
            workflow_id="wf.b",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            idempotency_key="shared-key",
        )
    retried, retried_replay, _ = _retry(session_factory, parent_id, idempotency_key="shared-key")
    assert prepared_replay is False
    assert retried_replay is False
    assert prepared.id != retried.id


# --------------------------------------------------------------------------------------
# v2-only edge cases: environment removal and insufficient role. `retry_operation` is
# `admin`-only (authorization.ROLE_CAPABILITIES); both cases need real org/environment/
# membership setup, unlike the v1-only matrix above.
# --------------------------------------------------------------------------------------


def _v2_world(session_factory: sessionmaker[Session], registry_path: Path) -> dict[str, Any]:
    with session_scope(session_factory) as session:
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)
        org = OrganizationRepository(session).create(name="Acme")
        environment = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
        )
        admin = PrincipalRepository(session).create(kind="user", display_name="Admin")
        operator = PrincipalRepository(session).create(kind="user", display_name="Operator")
        memberships = OrganizationMembershipRepository(session)
        memberships.create(principal_id=admin.id, organization_id=org.id, roles=["admin"])
        memberships.create(principal_id=operator.id, organization_id=org.id, roles=["operator"])
        return {
            "org_id": org.id,
            "env_id": environment.id,
            "admin_id": admin.id,
            "operator_id": operator.id,
        }


def test_retry_after_environment_archived_since_parent_refuses(
    session_factory: sessionmaker[Session], registry_path: Path
) -> None:
    world = _v2_world(session_factory, registry_path)
    with session_scope(session_factory) as session:
        operation, _, _ = service.prepare_operation(
            session,
            principal_id=world["admin_id"],
            environment=world["env_id"],
            workflow_id="wf.b",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        parent_id = operation.id
        service.execute_operation(
            session,
            operation_id=parent_id,
            handle=parent_id,
            principal_id=world["admin_id"],
            preflight=FakePreflight(),
            enable_v2=True,
        )
        service.record_execution_outcome(
            session, operation_id=parent_id, outcome="error", error={"message": "boom"}
        )
        EnvironmentRepository(session).archive(world["env_id"])

    with (
        session_scope(session_factory) as session,
        pytest.raises(EnvironmentArchivedError),
    ):
        service.retry_operation(
            session,
            operation_id=parent_id,
            principal_id=world["admin_id"],
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )


def test_retry_by_a_non_admin_role_is_not_found(
    session_factory: sessionmaker[Session], registry_path: Path
) -> None:
    world = _v2_world(session_factory, registry_path)
    with session_scope(session_factory) as session:
        operation, _, _ = service.prepare_operation(
            session,
            principal_id=world["admin_id"],
            environment=world["env_id"],
            workflow_id="wf.b",
            arguments={},
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
        parent_id = operation.id
        service.execute_operation(
            session,
            operation_id=parent_id,
            handle=parent_id,
            principal_id=world["admin_id"],
            preflight=FakePreflight(),
            enable_v2=True,
        )
        service.record_execution_outcome(
            session, operation_id=parent_id, outcome="error", error={"message": "boom"}
        )

    with (
        session_scope(session_factory) as session,
        pytest.raises(OperationNotFoundError),
    ):
        service.retry_operation(
            session,
            operation_id=parent_id,
            principal_id=world["operator_id"],
            preflight=FakePreflight(),
            server_max_argument_bytes=262_144,
            enable_v2=True,
        )
