"""Persistence: SQLAlchemy 2.0 over SQLite in v1, PostgreSQL in v2.

Portable constructs only — ULID string keys, timezone-aware UTC timestamps, the generic
``JSON`` type, no engine-specific SQL, every schema change an Alembic migration. The
binding rules are D1-D10 in ADR-004; tables are specified in BUILD_PLAN section 8.1.
"""
