"""SQLAlchemy 2.0 ORM models (typed declarative style).

Tables: principals, registry_snapshots, workflow_bindings, operations,
operation_events, approvals, execution_results, audit_log
(BUILD_PLAN section 8.1).

Uniqueness is enforced by database constraints, not application checks (ADR-004 D8).

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations
