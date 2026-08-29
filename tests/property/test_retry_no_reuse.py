"""Invariant I11 / ADR-012 section 1 (stage 06), as Hypothesis properties against a
real database: no retry path ever mutates its parent, no operation's approval row is
ever shared with another operation, ``UNKNOWN`` (and every other terminal state) never
gains an outgoing transition, and a chain of retries is always a simple, acyclic
lineage — proven by actually generating one through the real service layer, not just
re-checking ``state_machine.TRANSITIONS`` in isolation.
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
from n8n_operator.core.state_machine import TERMINAL_STATES
from n8n_operator.storage.models import Base
from n8n_operator.storage.repository import (
    ApprovalRepository,
    OperationEventRepository,
    OperationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: retry-property-test
workflows:
  - id: wf.a
    n8n_workflow_id: n8n-1
    title: Campaign dispatch
    description: Read-only, auto-approved.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_a}
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
      properties: {{}}
      additionalProperties: false
    limits:
      approval_ttl_seconds: 900
      execution_ttl_seconds: 300
  - id: wf.b
    n8n_workflow_id: n8n-2
    title: Needs approval
    description: External write.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_b}
    risk: medium
    side_effects: external_write
    approval: required
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


class _FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


class _Env:
    """A fresh, isolated SQLite database with the registry loaded — one instance per
    Hypothesis example."""

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
            PrincipalRepository(session).create(id="local", kind="local", display_name="local")
            service.reload_registry(session, self.registry_path, server_max_argument_bytes=262_144)

    def make_failed(self, *, workflow_id: str = "wf.a") -> str:
        with session_scope(self.session_factory) as session:
            operation, _, _ = service.prepare_operation(
                session,
                principal_id="local",
                environment="default",
                workflow_id=workflow_id,
                arguments={},
                preflight=_FakePreflight(),
                server_max_argument_bytes=262_144,
            )
            if workflow_id == "wf.b":
                service.approve_operation(session, operation_id=operation.id, decided_by="local")
            service.execute_operation(
                session,
                operation_id=operation.id,
                handle=operation.id,
                principal_id="local",
                preflight=_FakePreflight(),
            )
            service.record_execution_outcome(
                session, operation_id=operation.id, outcome="error", error={"message": "boom"}
            )
            return operation.id

    def retry(self, operation_id: str) -> str:
        with session_scope(self.session_factory) as session:
            operation, _, _ = service.retry_operation(
                session,
                operation_id=operation_id,
                principal_id="local",
                preflight=_FakePreflight(),
                server_max_argument_bytes=262_144,
            )
            return operation.id

    def snapshot_row(self, operation_id: str) -> dict[str, Any]:
        with session_scope(self.session_factory) as session:
            row = OperationRepository(session).get(operation_id)
            assert row is not None
            return {
                "state": row.state,
                "handle_burned_at": row.handle_burned_at,
                "approval_expires_at": row.approval_expires_at,
                "approval_policy_snapshot": row.approval_policy_snapshot,
            }

    def close(self) -> None:
        self.engine.dispose()
        self.db_path.unlink(missing_ok=True)
        self.registry_path.unlink(missing_ok=True)


@given(chain_length=st.integers(min_value=1, max_value=4))
@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_no_retry_ever_mutates_its_parent(chain_length: int) -> None:
    env = _Env()
    try:
        root_id = env.make_failed()
        chain = [root_id]
        snapshots_before_next_retry = {root_id: env.snapshot_row(root_id)}

        current_failed = root_id
        for _ in range(chain_length):
            new_id = env.retry(current_failed)
            with session_scope(env.session_factory) as session:
                service.execute_operation(
                    session,
                    operation_id=new_id,
                    handle=new_id,
                    principal_id="local",
                    preflight=_FakePreflight(),
                )
                service.record_execution_outcome(
                    session, operation_id=new_id, outcome="error", error={"message": "boom"}
                )
            chain.append(new_id)
            snapshots_before_next_retry[new_id] = env.snapshot_row(new_id)
            current_failed = new_id

        # Every ancestor's own row must be exactly what it was right after it became
        # FAILED — no later retry of a descendant ever wrote back to it.
        for operation_id in chain[:-1]:
            assert env.snapshot_row(operation_id) == snapshots_before_next_retry[operation_id]

        # The lineage graph is a simple chain: walking parent_operation_id from the
        # newest node reaches None in exactly `len(chain) - 1` steps, never fewer
        # (would imply a skipped link) or more (would imply a cycle/dangling walk).
        with session_scope(env.session_factory) as session:
            op_repo = OperationRepository(session)
            node_id: str | None = chain[-1]
            steps = 0
            visited: set[str] = set()
            while node_id is not None:
                assert node_id not in visited, "cycle detected in retry lineage"
                visited.add(node_id)
                row = op_repo.get(node_id)
                assert row is not None
                node_id = row.parent_operation_id
                steps += 1
        assert steps == len(chain)
    finally:
        env.close()


def test_approval_rows_are_never_shared_across_operations() -> None:
    env = _Env()
    try:
        root_id = env.make_failed(workflow_id="wf.b")
        child_id = env.retry(root_id)
        with session_scope(env.session_factory) as session:
            approvals = ApprovalRepository(session)
            root_approvals = {a.id for a in [approvals.get_by_operation_id(root_id)] if a}
            child_approvals = {a.id for a in [approvals.get_by_operation_id(child_id)] if a}
        assert root_approvals.isdisjoint(child_approvals)
    finally:
        env.close()


def test_unknown_and_every_terminal_state_never_gains_an_outgoing_transition() -> None:
    env = _Env()
    try:
        root_id = env.make_failed()
        child_id = env.retry(root_id)
        with session_scope(env.session_factory) as session:
            events = OperationEventRepository(session).list_for_operation(
                root_id
            ) + OperationEventRepository(session).list_for_operation(child_id)
        for event in events:
            if event.from_state is not None:
                assert event.from_state not in TERMINAL_STATES, (
                    f"transition {event.transition} recorded {event.from_state!r} (a "
                    "terminal state) as a from_state — terminal states have no "
                    "outgoing edge (invariant I2/I7)"
                )
    finally:
        env.close()
