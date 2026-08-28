"""Builds a realistic, populated v1 database: every operation state, approvals, a
result, a registry snapshot, and a real hash-chained audit log — the fixture the
migration tests copy from a real SQLite file to a real PostgreSQL database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from n8n_operator.audit.writer import write as audit_write
from n8n_operator.storage.models import STATES, new_ulid
from n8n_operator.storage.repository import (
    ApprovalRepository,
    AuditLogRepository,
    ExecutionResultRepository,
    OperationRepository,
    PrincipalRepository,
    RegistrySnapshotRepository,
)
from n8n_operator.storage.session import session_scope


def seed_full_v1_fixture(session_factory: sessionmaker[Session]) -> dict[str, Any]:
    """Every v1 state (BUILD_PLAN section 5.1), one operation each, plus one approval,
    one execution result, one registry snapshot, and a real audit chain of one entry per
    operation. Returns the created IDs for tests to assert against post-migration."""
    now = datetime.now(UTC)
    operation_ids: dict[str, str] = {}

    with session_scope(session_factory) as session:
        principal = PrincipalRepository(session).create(kind="local", display_name="local")
        snapshot = RegistrySnapshotRepository(session).create(
            content_hash="sha256:" + "a" * 64,
            source_path="./workflows.yaml",
            document={"apiVersion": "n8n-operator/v1", "workflows": []},
        )

        for state in STATES:
            op_id = f"op_{new_ulid()}"
            OperationRepository(session).create(
                id=op_id,
                principal_id=principal.id,
                environment="default",
                snapshot_id=snapshot.id,
                workflow_id="fixture.workflow",
                definition_hash="sha256:" + "b" * 64,
                state=state,
                arguments={"unicode": "café ☃ — em dash", "n": 1},
                argument_fingerprint="sha256:" + "c" * 64,
                argument_bytes=42,
                approval_expires_at=now + timedelta(minutes=10),
                execution_deadline=now + timedelta(minutes=15),
            )
            operation_ids[state] = op_id
            audit_write(
                AuditLogRepository(session),
                actor=principal.id,
                action="prepare_operation",
                subject_type="operation",
                subject_id=op_id,
                outcome="allowed",
                detail={"state": state},
                occurred_at=now,
            )

        pending_op_id = operation_ids["PENDING_APPROVAL"]
        ApprovalRepository(session).create(
            operation_id=pending_op_id,
            token_hash="sha256:" + "d" * 64,
            binding_hash="sha256:" + "e" * 64,
            expires_at=now + timedelta(minutes=10),
        )

        succeeded_op_id = operation_ids["SUCCEEDED"]
        ExecutionResultRepository(session).create(
            operation_id=succeeded_op_id,
            status="success",
            started_at=now,
            finished_at=now + timedelta(seconds=3),
            redacted_payload={"ok": True, "unicode": "résumé 日本語"},
            node_trace={"nodes": [{"name": "Webhook", "status": "success"}]},
        )

        audit_write(
            AuditLogRepository(session),
            actor=principal.id,
            action="registry_reload",
            subject_type="registry",
            subject_id=snapshot.id,
            outcome="allowed",
            detail={},
            occurred_at=now,
        )

    return {
        "principal_id": principal.id,
        "snapshot_id": snapshot.id,
        "operation_ids": operation_ids,
        "approval_operation_id": pending_op_id,
        "result_operation_id": succeeded_op_id,
    }


__all__ = ["seed_full_v1_fixture"]
