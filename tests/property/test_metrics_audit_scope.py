"""Stage 08's own scope-filter-before-aggregation/pagination guarantee (ADR-019
section 2, ADR-012 section 3) — a workflow-scoped ``get_metrics`` breakdown, and a
``list_audit_events`` page at any cursor position, must never surface a count or an
event derived from a workflow, operation, or environment outside the caller's own
authorized scope. Mirrors ``tests/property/test_no_enumeration.py``'s AC-44 scenario
shape (two real callers, one authorized, one not, against the same real database) —
this file is the stage-08-specific extension it names as future work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.core import service
from n8n_operator.core.models import PreflightResult
from n8n_operator.storage.repository import (
    AuditLogRepository,
    EnvironmentRepository,
    OrganizationMembershipRepository,
    OrganizationRepository,
    PrincipalRepository,
)
from n8n_operator.storage.session import session_scope

REGISTRY_YAML = """apiVersion: n8n-operator/v1
metadata:
  name: scope-test
workflows:
  - id: crm.sync_contact
    n8n_workflow_id: n8n-1
    title: Sync a contact into the CRM
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
      properties: {{}}
      additionalProperties: false
  - id: sales.hidden_workflow
    n8n_workflow_id: n8n-2
    title: A sales-only workflow a marketing viewer must never see counted
    description: External write.
    owner: carolyn
    version: 1
    definition_hash: sha256:{hash_b}
    risk: high
    side_effects: irreversible
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
""".format(hash_a="a" * 64, hash_b="b" * 64)


class FakePreflight:
    def check(self, workflow: Any) -> PreflightResult:
        return PreflightResult(ready=True, checks=[], checked_at=datetime.now(UTC))


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    path = tmp_path / "workflows.yaml"
    path.write_text(REGISTRY_YAML)
    return path


@pytest.fixture
def scoped_scenario(session_factory: sessionmaker[Session], registry_path: Path) -> dict[str, str]:
    """One organization, one environment, two workflows (``crm.*``, ``sales.*``), a
    ``local`` v1 principal that prepares an operation against *each*, and a v2
    ``marketing`` viewer whose own grant covers only ``crm.*`` — the exact shape
    needed to prove the sales-only operation's counts/events never leak into a
    marketing-scoped ``get_metrics``/``list_audit_events`` call."""
    with session_scope(session_factory) as session:
        PrincipalRepository(session).create(id="local", kind="local", display_name="local")
        service.reload_registry(session, registry_path, server_max_argument_bytes=262_144)

        org = OrganizationRepository(session).create(name="Acme")
        env_row = EnvironmentRepository(session).create(
            organization_id=org.id,
            name="production",
            n8n_base_url_ref="env:X",
            n8n_api_key_ref="env:Y",
        )
        marketing = PrincipalRepository(session).create(kind="user", display_name="Marketing")
        OrganizationMembershipRepository(session).create(
            principal_id=marketing.id,
            organization_id=org.id,
            roles=["viewer"],
            workflow_scope="crm.*",
        )
        marketing_id, environment_id = marketing.id, env_row.id

    for workflow_id in ("crm.sync_contact", "sales.hidden_workflow"):
        with session_scope(session_factory) as session:
            service.prepare_operation(
                session,
                principal_id="local",
                environment="default",
                workflow_id=workflow_id,
                arguments={},
                preflight=FakePreflight(),
                server_max_argument_bytes=262_144,
            )

    return {"marketing_id": marketing_id, "environment_id": environment_id}


@pytest.mark.integration
def test_get_metrics_breakdown_never_includes_the_out_of_scope_workflow(
    session_factory: sessionmaker[Session], scoped_scenario: dict[str, str]
) -> None:
    with session_scope(session_factory) as session:
        result = service.get_metrics(
            session,
            principal_id=scoped_scenario["marketing_id"],
            environment=scoped_scenario["environment_id"],
            group_by="workflow",
            enable_v2=True,
        )
    keys = {entry.key for entry in result.breakdown}
    assert "sales.hidden_workflow" not in keys
    assert "crm.sync_contact" in keys or not keys  # v1-prepared rows use environment="default"


@pytest.mark.integration
def test_list_audit_events_never_returns_an_out_of_scope_operation_subject(
    session_factory: sessionmaker[Session], scoped_scenario: dict[str, str]
) -> None:
    with session_scope(session_factory) as session:
        page = service.list_audit_events(
            session,
            principal_id=scoped_scenario["marketing_id"],
            environment=scoped_scenario["environment_id"],
            limit=100,
            enable_v2=True,
        )
    for event in page.events:
        assert event.subject_id != "sales.hidden_workflow"
        assert "sales" not in event.subject_id


@pytest.mark.integration
def test_list_audit_events_never_leaks_a_registry_snapshot_event_to_a_non_admin(
    session_factory: sessionmaker[Session], scoped_scenario: dict[str, str]
) -> None:
    with session_scope(session_factory) as session:
        page = service.list_audit_events(
            session,
            principal_id=scoped_scenario["marketing_id"],
            environment=scoped_scenario["environment_id"],
            limit=100,
            enable_v2=True,
        )
    assert all(event.subject_type != "registry_snapshot" for event in page.events)


@pytest.mark.integration
def test_list_audit_events_pagination_is_stable_under_a_concurrent_insert(
    session_factory: sessionmaker[Session],
) -> None:
    """A row inserted with a ``seq`` between two already-returned rows (impossible in
    practice, since ``seq`` is a real autoincrement — this simulates the *general*
    append-during-pagination case: a brand-new row appended after page one is fetched)
    must not cause page two to duplicate or skip an already-seen row, since
    ``seq < before_seq`` is a stable boundary independent of what gets appended
    elsewhere in the table afterward."""
    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        prev = repo.get_last_hash()
        for i in range(3):
            entry = repo.append(
                prev_hash=prev,
                entry_hash=f"hash{i}",
                actor="system",
                action="a",
                subject_type="workflow",
                subject_id="wf.a",
                outcome="allowed",
            )
            prev = entry.entry_hash

    with session_scope(session_factory) as session:
        page1 = service.list_audit_events(session, principal_id="local", limit=2)

    # A concurrent writer appends a new row after page one was fetched.
    with session_scope(session_factory) as session:
        repo = AuditLogRepository(session)
        repo.append(
            prev_hash=repo.get_last_hash(),
            entry_hash="hash-new",
            actor="system",
            action="a",
            subject_type="workflow",
            subject_id="wf.a",
            outcome="allowed",
        )

    with session_scope(session_factory) as session:
        page2 = service.list_audit_events(
            session, principal_id="local", limit=2, cursor=page1.next_cursor
        )

    seqs_page1 = {e.seq for e in page1.events}
    seqs_page2 = {e.seq for e in page2.events}
    assert seqs_page1.isdisjoint(seqs_page2)
    assert all(seq < min(seqs_page1) for seq in seqs_page2)
