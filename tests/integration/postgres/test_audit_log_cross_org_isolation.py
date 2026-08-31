"""``AuditLogRepository.list_page``'s cross-organization scoping (Stage 11 security
review) against real PostgreSQL — the same two-organization, three-environment,
shared-workflow-id scenario ``tests/integration/test_metrics_audit_repository.py``
exercises against SQLite, mirrored here (``test_quorum_concurrency.py``'s own
pattern) because organization/environment isolation is exactly the kind of
correctness property SQLite's single-writer model can't meaningfully distinguish
from an accident of query planning — a real Postgres index/plan choice is the
strongest evidence the fix is not an artifact of SQLite's simpler execution path.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from n8n_operator.storage.repository import (
    AuditLogRepository,
    EnvironmentRepository,
    OperationRepository,
    OrganizationRepository,
    PrincipalRepository,
    RegistrySnapshotRepository,
)
from n8n_operator.storage.session import (
    create_engine_for_url,
    create_session_factory,
    session_scope,
)

pytestmark = pytest.mark.postgres


def _migrated_engine(url: str) -> Engine:
    from alembic import command

    from n8n_operator.cli.commands.db import _alembic_config

    command.upgrade(_alembic_config(url), "head")
    return create_engine_for_url(url, pool_size=5, max_overflow=5)


def _seed_operation(
    session: Session,
    *,
    principal_id: str,
    snapshot_id: str,
    op_id: str,
    workflow_id: str,
    environment_id: str,
    organization_id: str,
) -> None:
    OperationRepository(session).create(
        id=op_id,
        principal_id=principal_id,
        environment=environment_id,
        environment_id=environment_id,
        organization_id=organization_id,
        snapshot_id=snapshot_id,
        workflow_id=workflow_id,
        definition_hash="sha256:" + "a" * 64,
        state="SUCCEEDED",
        arguments={},
        argument_fingerprint=f"fp-{op_id}",
        argument_bytes=2,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "pattern",
    ["%", "crm.shared", "crm.%"],
    ids=["wildcard", "exact", "prefix"],
)
def test_list_page_never_crosses_org_boundary_on_real_postgres(
    postgres_test_db_url: str, pattern: str
) -> None:
    engine = _migrated_engine(postgres_test_db_url)
    session_factory = create_session_factory(engine)
    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).create(kind="local", display_name="local")
        snapshot = RegistrySnapshotRepository(session).create(
            content_hash="sha256:" + "b" * 64, source_path="./workflows.yaml", document={}
        )

        org_a = OrganizationRepository(session).create(name="org-a")
        org_b = OrganizationRepository(session).create(name="org-b")
        env_a_staging = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="staging",
            n8n_base_url_ref="env:A_STAGING_URL",
            n8n_api_key_ref="env:A_STAGING_KEY",
        )
        env_a_prod = EnvironmentRepository(session).create(
            organization_id=org_a.id,
            name="production",
            n8n_base_url_ref="env:A_PROD_URL",
            n8n_api_key_ref="env:A_PROD_KEY",
        )
        env_b_prod = EnvironmentRepository(session).create(
            organization_id=org_b.id,
            name="production",
            n8n_base_url_ref="env:B_PROD_URL",
            n8n_api_key_ref="env:B_PROD_KEY",
        )

        _seed_operation(
            session,
            principal_id=principal.id,
            snapshot_id=snapshot.id,
            op_id="op_a_staging",
            workflow_id="crm.shared",
            environment_id=env_a_staging.id,
            organization_id=org_a.id,
        )
        _seed_operation(
            session,
            principal_id=principal.id,
            snapshot_id=snapshot.id,
            op_id="op_a_prod",
            workflow_id="crm.shared",
            environment_id=env_a_prod.id,
            organization_id=org_a.id,
        )
        _seed_operation(
            session,
            principal_id=principal.id,
            snapshot_id=snapshot.id,
            op_id="op_b_prod",
            workflow_id="crm.shared",
            environment_id=env_b_prod.id,
            organization_id=org_b.id,
        )

        repo = AuditLogRepository(session)
        for op_id in ("op_a_staging", "op_a_prod", "op_b_prod"):
            repo.append(
                prev_hash=repo.get_last_hash(),
                entry_hash=f"h-{op_id}",
                actor="system",
                action="operation.prepared",
                subject_type="operation",
                subject_id=op_id,
                outcome="allowed",
            )

        rows_a = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=[pattern],
            environment_id=env_a_prod.id,
            include_registry_snapshot_events=False,
        )
        rows_b = repo.list_page(
            before_seq=None,
            limit=100,
            since=None,
            workflow_id=None,
            workflow_id_like_patterns=[pattern],
            environment_id=env_b_prod.id,
            include_registry_snapshot_events=False,
        )

    engine.dispose()
    assert {r.subject_id for r in rows_a} == {"op_a_prod"}
    assert {r.subject_id for r in rows_b} == {"op_b_prod"}
