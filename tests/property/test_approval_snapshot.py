"""Invariant I13 / ADR-017 section 1 (stage 05), as Hypothesis properties against a
real database: an operation's approval-policy snapshot is fixed the moment it enters
``PENDING_APPROVAL`` and never gains members afterward, a decision already cast
survives the decider's later removal, and the requester is excluded from their own
snapshot regardless of what other roles they hold.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.storage.models import Base
from n8n_operator.storage.repository import (
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: snapshot-property-test
workflows:
  - id: crm.bulk_update_stage
    n8n_workflow_id: n8n-1
    title: Bulk-update deal stage
    description: Production-only, quorum-gated.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
    risk: high
    side_effects: external_write
    approval: required
    trigger:
      type: webhook
      method: POST
      path: /webhook/a
      auth: none
    input_schema:
      type: object
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
      quorum_count: 2
""".format(hash_a="a" * 64)


class _FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


class _Env:
    """A fresh, isolated SQLite database, one org/environment, and the loaded
    quorum-2 registry — one instance per Hypothesis example."""

    def __init__(self) -> None:
        fd, path_str = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = Path(path_str)
        self.db_path.unlink()
        self.engine = create_engine_for_url(f"sqlite+pysqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.session_factory = create_session_factory(self.engine)

        registry_fd, registry_path_str = tempfile.mkstemp(suffix=".yaml")
        os.close(registry_fd)
        self.registry_path = Path(registry_path_str)
        self.registry_path.write_text(REGISTRY_YAML)

        with session_scope(self.session_factory) as session:
            service.reload_registry(session, self.registry_path, server_max_argument_bytes=262_144)
            org = OrganizationRepository(session).create(name="Acme")
            env = EnvironmentRepository(session).create(
                organization_id=org.id,
                name="production",
                n8n_base_url_ref="env:X",
                n8n_api_key_ref="env:Y",
                is_production=True,
            )
            self.org_id, self.env_id = org.id, env.id

    def add_principal(self, *, roles: list[str], display_name: str = "P") -> str:
        with session_scope(self.session_factory) as session:
            principal = PrincipalRepository(session).create(kind="user", display_name=display_name)
            OrganizationMembershipRepository(session).create(
                principal_id=principal.id, organization_id=self.org_id, roles=roles
            )
            return principal.id

    def remove_membership(self, principal_id: str) -> None:
        with session_scope(self.session_factory) as session:
            memberships = OrganizationMembershipRepository(session)
            membership = memberships.get_active(
                principal_id=principal_id, organization_id=self.org_id
            )
            assert membership is not None
            memberships.remove(membership.id)

    def prepare(self, *, requester_id: str) -> str:
        with session_scope(self.session_factory) as session:
            operation, _, _ = service.prepare_operation(
                session,
                principal_id=requester_id,
                environment=self.env_id,
                workflow_id="crm.bulk_update_stage",
                arguments={},
                preflight=_FakePreflight(),
                server_max_argument_bytes=262_144,
                enable_v2=True,
            )
            return operation.id

    def snapshot(self, *, operation_id: str, principal_id: str) -> list[str]:
        with session_scope(self.session_factory) as session:
            status = service.get_approval_status(
                session, operation_id=operation_id, principal_id=principal_id, enable_v2=True
            )
            return status.approval_policy_snapshot

    def close(self) -> None:
        self.engine.dispose()
        self.db_path.unlink(missing_ok=True)
        self.registry_path.unlink(missing_ok=True)


@given(
    num_initial_approvers=st.integers(min_value=1, max_value=3),
    num_added_later=st.integers(min_value=1, max_value=3),
)
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_membership_added_after_snapshot_never_joins_it(
    num_initial_approvers: int, num_added_later: int
) -> None:
    env = _Env()
    try:
        requester_id = env.add_principal(roles=["operator"], display_name="requester")
        initial_approvers = {
            env.add_principal(roles=["approver"], display_name=f"initial-{i}")
            for i in range(num_initial_approvers)
        }
        operation_id = env.prepare(requester_id=requester_id)
        snapshot_before = set(env.snapshot(operation_id=operation_id, principal_id=requester_id))
        assert snapshot_before == initial_approvers

        for i in range(num_added_later):
            env.add_principal(roles=["approver"], display_name=f"late-{i}")

        snapshot_after = set(env.snapshot(operation_id=operation_id, principal_id=requester_id))
        assert snapshot_after == snapshot_before
    finally:
        env.close()


@given(num_approvers=st.integers(min_value=2, max_value=3))
@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_a_decision_cast_survives_the_deciders_later_removal(num_approvers: int) -> None:
    env = _Env()
    try:
        requester_id = env.add_principal(roles=["operator"], display_name="requester")
        approvers = [
            env.add_principal(roles=["approver"], display_name=f"a-{i}")
            for i in range(num_approvers)
        ]
        operation_id = env.prepare(requester_id=requester_id)
        decider = approvers[0]
        with session_scope(env.session_factory) as session:
            service.approve_operation(
                session, operation_id=operation_id, decided_by=decider, enable_v2=True
            )
        env.remove_membership(decider)

        with session_scope(env.session_factory) as session:
            status = service.get_approval_status(
                session, operation_id=operation_id, principal_id=requester_id, enable_v2=True
            )
        assert decider in [d.principal_id for d in status.decisions]
        assert decider in status.approval_policy_snapshot  # I13: the slot itself stays too
    finally:
        env.close()


@given(
    extra_roles=st.lists(
        st.sampled_from(["viewer", "operator", "approver", "admin"]), max_size=3, unique=True
    )
)
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_requester_is_excluded_from_their_own_snapshot_regardless_of_other_roles(
    extra_roles: list[str],
) -> None:
    env = _Env()
    try:
        roles = list({*extra_roles, "operator"})
        requester_id = env.add_principal(roles=roles, display_name="requester")
        env.add_principal(roles=["approver"], display_name="other-approver")
        operation_id = env.prepare(requester_id=requester_id)
        snapshot = env.snapshot(operation_id=operation_id, principal_id=requester_id)
        assert requester_id not in snapshot
    finally:
        env.close()
