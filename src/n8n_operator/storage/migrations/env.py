"""Alembic migration environment.

The schema is created by migrations, never by ``create_all`` outside tests
(ADR-004 D6). AC-24 requires that autogenerate against the ORM metadata produce an
empty diff, so schema and models cannot silently diverge.

Phase 1 (BUILD_PLAN section 12).
"""

from __future__ import annotations
